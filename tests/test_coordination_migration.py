"""Migration of *legacy* stores plus the remaining C11 recovery coverage (tic-d047).

Two complementary things are protected here:

1. **Legacy stores migrate without conjuring coordination.** A pre-coordination
   file project (markdown tickets in the status folders, no `.arbite/coordination`)
   and a pre-coordination SQLite v1 database (tickets and `schema_version`, no
   `tickets.revision`, no coordination tables) are built with raw files and raw SQL
   -- exactly the bytes the old code wrote -- and each is migrated into the other
   sink. Ticket content must survive and *no* attempt/claim/receipt/intent may be
   invented; the only coordination event that ever appears is the real
   `workspace_bound` from a genuine `workspace.ensure_binding` at bind time.

2. **The recovery/refusal behaviour around migration.** The doctor on a legacy
   store reports nothing and creates nothing; corrupt and missing artifacts are
   reported and never repaired or deleted; pending intents are observed against
   the real workspace bytes and only the unambiguous cases are fixed; a failure
   injected mid-migration leaves the source byte-identical and the destination
   unbound; every active state refuses the transfer before anything is copied; and
   re-importing or re-running a migration is idempotent.

The doctor/refusal cases run against both shipped sinks wherever the semantics are
shared, so "the same store behaves the same way" stays a checked claim.
"""

from __future__ import annotations

import copy
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from arbite import application, cli, coordination as c, coordination_export as x
from arbite import workspace as w
from arbite.application import Actor
from arbite.coordination_doctor import coordination_problems
from arbite.errors import CoordinationConflict
from arbite.sinks import SQLITE_FILENAME, SinkSpec, build_sink
from arbite.sinks.file import FileSink
from arbite.sinks.sqlite import DDL, SCHEMA_VERSION, SqliteSink
from conftest import make_sink
from helpers import make_ticket

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
KINDS = ("file", "sqlite")


def other_kind(kind: str) -> str:
    return "sqlite" if kind == "file" else "file"


# ---------------------------------------------------------------------------
# CLI runner (same conventions as tests/test_cli.py)
# ---------------------------------------------------------------------------


def run_cli(project, *args, sink=None, expect=0):
    environment = dict(os.environ, PYTHONPATH=str(SRC_DIR))
    environment.pop("ARBITE_SINK", None)
    if sink:
        environment["ARBITE_SINK"] = sink
    proc = subprocess.run(
        [sys.executable, "-m", "arbite.cli", *args],
        cwd=str(project),
        env=environment,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == expect, (
        f"arbite {' '.join(args)} -> exit {proc.returncode}, expected {expect}\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    return proc


def config_bytes(project_root: Path) -> bytes:
    path = project_root / "arbite.yaml"
    return path.read_bytes() if path.exists() else b""


def read_marker(project_root: Path) -> dict:
    return json.loads((project_root / ".arbite" / "workspace-binding.json").read_text())


# ---------------------------------------------------------------------------
# Raw-storage helpers (never go through the API)
# ---------------------------------------------------------------------------


def _connect(arbite_dir) -> sqlite3.Connection:
    conn = sqlite3.connect(str(arbite_dir / SQLITE_FILENAME))
    conn.row_factory = sqlite3.Row
    return conn


def _tables(db_path: Path) -> set:
    conn = sqlite3.connect(str(db_path))
    try:
        return {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    finally:
        conn.close()


def _columns(db_path: Path, table: str) -> set:
    conn = sqlite3.connect(str(db_path))
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


def _artifact_path(arbite_dir: Path, digest: str) -> Path:
    return arbite_dir / "coordination" / "artifacts" / digest[len("sha256:"):]


def tamper_artifact_blob(kind: str, arbite_dir: Path, digest: str, data: bytes) -> None:
    """Overwrite the stored blob for `digest` with `data`, bypassing the API."""
    if kind == "file":
        _artifact_path(arbite_dir, digest).write_bytes(data)
        return
    conn = _connect(arbite_dir)
    try:
        conn.execute(
            "UPDATE coordination_artifacts SET content = ? WHERE digest = ?",
            (sqlite3.Binary(data), digest),
        )
        conn.commit()
    finally:
        conn.close()


def remove_artifact_blob(kind: str, arbite_dir: Path, digest: str) -> None:
    """Delete the stored blob for `digest`, leaving the record referencing it."""
    if kind == "file":
        _artifact_path(arbite_dir, digest).unlink()
        return
    conn = _connect(arbite_dir)
    try:
        conn.execute("DELETE FROM coordination_artifacts WHERE digest = ?", (digest,))
        conn.commit()
    finally:
        conn.close()


def stored_artifact_bytes(kind: str, arbite_dir: Path, digest: str):
    if kind == "file":
        path = _artifact_path(arbite_dir, digest)
        return path.read_bytes() if path.exists() else None
    conn = _connect(arbite_dir)
    try:
        row = conn.execute(
            "SELECT content FROM coordination_artifacts WHERE digest = ?", (digest,)
        ).fetchone()
    finally:
        conn.close()
    return None if row is None else bytes(row[0])


# ---------------------------------------------------------------------------
# Building coordination state
# ---------------------------------------------------------------------------


def service_for(sink, arbite_dir):
    return application.coordination_service_for(
        sink, root=str(arbite_dir.parent), actor=Actor("tester")
    )


def populate(sink, arbite_dir):
    """A populated, *quiescent* store: binding, finished attempt, released claim,
    a receipt/intent referencing a stored artifact and one event."""
    service = service_for(sink, arbite_dir)
    store = sink.coordination()
    workspace = service.workspace
    sink.create(make_ticket("tic-a1b2", status="in_progress", assignee="tester"))
    now = c.utc_now()

    attempt = c.WorkAttempt(
        id=c.new_record_id("work_attempt"),
        ticket_id="tic-a1b2",
        worker_id="tester",
        workspace_id=workspace.id,
        generation=1,
        started=now,
        last_activity=now,
        state="finished",
        ended=now,
        outcome="ok",
    )
    data = b"hello evidence"
    descriptor = store.store_artifact_bytes(data, media_type="text/plain")
    artifact = c.Artifact(
        id=descriptor.id,
        digest=descriptor.digest,
        size=descriptor.size,
        created=now,
        location=descriptor.location,
        media_type=descriptor.media_type,
    )
    claim = c.FileClaim(
        id=c.new_record_id("file_claim"),
        workspace_id=workspace.id,
        path="src/a.py",
        ticket_id="tic-a1b2",
        attempt_id=attempt.id,
        generation=1,
        acquired=now,
        observed_version=c.digest_of_bytes(b"old"),
        state="released",
        released=now,
    )
    receipt = c.OperationReceipt(
        id=c.new_operation_id(),
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
    intent = c.OperationIntent(
        id=c.new_record_id("operation_intent"),
        operation_id=receipt.id,
        workspace_id=workspace.id,
        attempt_id=attempt.id,
        ticket_id="tic-a1b2",
        actor="tester",
        kind_="write",
        created=now,
        updated=now,
        before={"src/a.py": c.digest_of_bytes(b"old")},
        after={"src/a.py": c.digest_of_bytes(b"new")},
        paths=["src/a.py"],
        artifact_refs=[artifact.id],
        claim_generation=1,
        state="finalized",
    )
    event = c.Event(
        id=c.new_record_id("event"),
        cursor=None,
        kind_="attempt_finished",
        category="lifecycle",
        timestamp=now,
        subject_ids=[attempt.id, "tic-a1b2"],
        payload={"workspace_id": workspace.id, "generation": 1},
    )
    with store.transaction() as tx:
        tx.put(attempt)
        tx.put(artifact)
        tx.put(claim)
        tx.put(receipt)
        tx.put(intent)
        tx.append_event(event)

    return {
        "service": service,
        "store": store,
        "workspace": workspace,
        "attempt": attempt,
        "artifact": artifact,
        "claim": claim,
        "receipt": receipt,
        "intent": intent,
        "event": event,
    }


def seed_coordination(project_root: Path, kind: str, *, blocker: str = "", active: bool = False):
    """A real project's coordination state, seeded through the real service.

    The base state is quiescent (a finished attempt, a stored artifact and a
    receipt); `blocker` then adds exactly one live thing so a refusal test can
    name it, and `active=True` is the shorthand for an active attempt.
    """
    arbite = project_root / ".arbite"
    arbite.mkdir(parents=True, exist_ok=True)
    sink = make_sink(kind, arbite)
    service = service_for(sink, arbite)
    store = sink.coordination()
    workspace = service.workspace
    sink.create(make_ticket("tic-a1b2", status="in_progress", assignee="tester"))
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
    descriptor = store.store_artifact_bytes(b"seed evidence", media_type="text/plain")
    artifact = c.Artifact(
        id=descriptor.id,
        digest=descriptor.digest,
        size=descriptor.size,
        created=now,
        location=descriptor.location,
        media_type=descriptor.media_type,
    )
    receipt = c.OperationReceipt(
        id=c.new_operation_id(),
        attempt_id=attempt.id,
        ticket_id="tic-a1b2",
        actor="tester",
        kind_="write",
        timestamp=now,
        artifact_refs=[artifact.id],
    )
    with store.transaction() as tx:
        tx.put(attempt)
        tx.put(artifact)
        tx.put(receipt)

    blocker_id = None
    if blocker == "attempt":
        live = c.WorkAttempt(
            id=c.new_record_id("work_attempt"),
            ticket_id="tic-a1b2",
            worker_id="tester",
            workspace_id=workspace.id,
            generation=2,
            started=now,
            last_activity=now,
        )
        with store.transaction() as tx:
            tx.put(live)
        blocker_id = live.id
    elif blocker == "claim":
        live_claim = c.FileClaim(
            id=c.new_record_id("file_claim"),
            workspace_id=workspace.id,
            path="src/busy.py",
            ticket_id="tic-a1b2",
            attempt_id=attempt.id,
            generation=2,
            acquired=now,
            observed_version=c.digest_of_bytes(b"old"),
        )
        with store.transaction() as tx:
            tx.put(live_claim)
        blocker_id = "src/busy.py"
    elif blocker == "intent":
        live_intent = c.OperationIntent(
            id=c.new_record_id("operation_intent"),
            operation_id=c.new_operation_id(),
            workspace_id=workspace.id,
            attempt_id=attempt.id,
            ticket_id="tic-a1b2",
            actor="tester",
            kind_="write",
            created=now,
            updated=now,
            before={"src/a.py": c.digest_of_bytes(b"old")},
            after={"src/a.py": c.digest_of_bytes(b"new")},
            paths=["src/a.py"],
            state="pending",
        )
        with store.transaction() as tx:
            tx.put(live_intent)
        blocker_id = live_intent.id

    return {
        "sink": sink,
        "store": store,
        "workspace": workspace,
        "attempt": attempt,
        "artifact": artifact,
        "receipt": receipt,
        "blocker_id": blocker_id,
    }


def bind_store(project_root: Path, kind: str):
    """The destination's first *real* coordination use: a genuine bind.

    Nothing in ticket migration binds the destination, so this is what makes
    `.arbite/coordination` appear lazily -- and the only `workspace_bound` event
    the destination should ever have.
    """
    arbite = project_root / ".arbite"
    sink = make_sink(kind, arbite)
    store = sink.coordination()
    resolution = w.ensure_binding(
        arbite,
        root=str(project_root),
        sink_kind=kind,
        location=str(sink.root),
        store=store,
    )
    return sink, store, resolution


def _find(store, kind: str) -> list:
    with store.transaction(write=False) as tx:
        return list(tx.find(kind))


def _revision_of(store, kind: str, record_id: str):
    for entry in store.inspect_records():
        if entry["kind"] == kind and entry["record_id"] == record_id:
            return entry["revision"]
    return None


def _ticket_snapshot(sink, ticket_id: str) -> dict:
    ticket = sink.get(ticket_id)
    return {
        "title": ticket.title,
        "status": ticket.status,
        "type": ticket.type,
        "tier": ticket.tier,
        "domain": ticket.domain,
        "tags": sorted(ticket.tags or []),
        "body": ticket.body,
        "bucket": sink.bucket(ticket_id),
    }


# ---------------------------------------------------------------------------
# 1. Legacy fixtures (raw bytes / raw SQL)
# ---------------------------------------------------------------------------

#: A ticket written the way the pre-coordination code wrote them: date-only
#: timestamps, a plain note line, tags in the frontmatter.
LEGACY_FILE_OPEN = """---
id: tic-a1b2
title: Legacy file ticket
status: open
type: bug
tier: medium
domain: mesh
created: 2026-01-01
updated: 2026-01-01
tags: [legacy, mesh]
depends_on: []
---

## Description
an old-style file ticket

## Notes
- 2026-01-02 someone: an old note
"""

LEGACY_FILE_WISHLIST = """---
id: tic-b2c3
title: Legacy shelved wish
status: shelved
type: feature
tier: low
domain: ui
created: 2026-01-02
updated: 2026-01-02
tags: [legacy]
depends_on: []
---

## Description
shelved and filed in a bucket

## Notes
"""

#: `(id, title, status, type, tier, domain, bucket, body, tags)` for the legacy db.
LEGACY_SQLITE_ROWS = (
    (
        "tic-c3d4",
        "Legacy database ticket",
        "open",
        "bug",
        "medium",
        "mesh",
        None,
        "## Description\nfrom the old database\n\n## Notes\n",
        ["db", "legacy"],
    ),
    (
        "tic-d4e5",
        "Legacy shelved row",
        "shelved",
        "feature",
        "low",
        "ui",
        "wishlist",
        "## Description\nshelved long ago\n\n## Notes\n",
        [],
    ),
)

LEGACY_IDS = {
    "file": ("tic-a1b2", "tic-b2c3"),
    "sqlite": ("tic-c3d4", "tic-d4e5"),
}


def build_legacy_sqlite(path: Path) -> None:
    """A database exactly as the pre-coordination code created it: the shipped
    ticket DDL (which has no `revision` column), a schema_version row, raw rows."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(DDL)
        conn.execute(
            "INSERT INTO schema_version (version) "
            "SELECT ? WHERE NOT EXISTS (SELECT 1 FROM schema_version)",
            (SCHEMA_VERSION,),
        )
        for ticket_id, title, status, type_, tier, domain, bucket, body, tags in (
            LEGACY_SQLITE_ROWS
        ):
            conn.execute(
                "INSERT INTO tickets "
                "(id, title, status, type, tier, domain, bucket, body, created, updated) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, '2026-01-01', '2026-01-01')",
                (ticket_id, title, status, type_, tier, domain, bucket, body),
            )
            conn.executemany(
                "INSERT INTO ticket_tags (ticket_id, tag, ordinal) VALUES (?, ?, ?)",
                [(ticket_id, tag, index) for index, tag in enumerate(tags)],
            )
        conn.commit()
    finally:
        conn.close()


def build_legacy_project(project_root: Path) -> Path:
    """An old-style project holding a legacy file layout *and* a legacy database."""
    arbite = project_root / ".arbite"
    for directory in ("raw", "open", "in_progress", "blocked", "shelved", "closed", "wishlist"):
        (arbite / directory).mkdir(parents=True, exist_ok=True)
    (arbite / "open" / "tic-a1b2.md").write_text(LEGACY_FILE_OPEN, encoding="utf-8")
    (arbite / "wishlist" / "tic-b2c3.md").write_text(LEGACY_FILE_WISHLIST, encoding="utf-8")
    build_legacy_sqlite(arbite / SQLITE_FILENAME)
    return arbite


def test_legacy_fixtures_are_really_pre_coordination(tmp_project):
    arbite = build_legacy_project(tmp_project)
    assert not (arbite / "coordination").exists()
    assert not (arbite / "workspace-binding.json").exists()

    db_path = arbite / SQLITE_FILENAME
    assert "coordination_records" not in _tables(db_path)
    assert "revision" not in _columns(db_path, "tickets")

    # Both old stores read their own tickets through the current sinks.
    assert sorted(FileSink(arbite).ids()) == ["tic-a1b2", "tic-b2c3"]
    assert sorted(SqliteSink(db_path).ids()) == ["tic-c3d4", "tic-d4e5"]


@pytest.mark.parametrize("source_kind", ("file", "sqlite"))
def test_legacy_fixture_migrates_into_the_other_sink_without_inventing_coordination(
    tmp_project, source_kind
):
    arbite = build_legacy_project(tmp_project)
    target_kind = other_kind(source_kind)
    source_sink = build_sink(SinkSpec(kind=source_kind), arbite)
    expected = {
        ticket_id: _ticket_snapshot(source_sink, ticket_id)
        for ticket_id in LEGACY_IDS[source_kind]
    }

    out = run_cli(tmp_project, "migrate", "--to", target_kind, sink=source_kind).stdout
    assert f"migrated 2 ticket(s) from {source_kind} to {target_kind}" in out
    # The source has no coordination work, so no transfer (and no rebind) ran.
    assert "transferred coordination history" not in out

    dest = build_sink(SinkSpec(kind=target_kind), arbite)
    for ticket_id, snapshot in expected.items():
        assert _ticket_snapshot(dest, ticket_id) == snapshot, ticket_id

    if target_kind == "sqlite":
        # The legacy database gained the coordination tables and the revision
        # column, and no attempt/claim was invented by the ticket copy.
        assert "coordination_records" in _tables(arbite / SQLITE_FILENAME)
        assert "coordination_events" in _tables(arbite / SQLITE_FILENAME)
        assert "revision" in _columns(arbite / SQLITE_FILENAME, "tickets")
        assert _find(dest.coordination(), "work_attempt") == []
        assert _find(dest.coordination(), "file_claim") == []
    else:
        # The file destination gains the layout only lazily, on first real use.
        assert not (arbite / "coordination").exists()

    # Bind the destination (its first real coordination use) and check that the
    # only coordination state is the real workspace_bound event.
    _dest_sink, dest_store, _resolution = bind_store(tmp_project, target_kind)
    if target_kind == "file":
        assert (arbite / "coordination").exists()
    assert {event.event_kind for event in dest_store.event_log()} == {"workspace_bound"}
    assert _find(dest_store, "work_attempt") == []
    assert _find(dest_store, "file_claim") == []
    assert _find(dest_store, "operation_receipt") == []
    assert _find(dest_store, "operation_intent") == []
    assert _find(dest_store, "artifact") == []
    assert dest_store.namespaces() == []


# ---------------------------------------------------------------------------
# 2. Doctor on a legacy store reports nothing and creates nothing
# ---------------------------------------------------------------------------


def test_legacy_store_doctor_reports_nothing_and_creates_nothing(tmp_project):
    arbite = build_legacy_project(tmp_project)
    file_sink = FileSink(arbite)
    sqlite_sink = SqliteSink(arbite / SQLITE_FILENAME)

    assert file_sink.check() == []
    assert sqlite_sink.check() == []
    assert coordination_problems(file_sink) == []
    assert coordination_problems(sqlite_sink) == []
    assert not (arbite / "coordination").exists()

    run_cli(tmp_project, "doctor", sink="file")
    run_cli(tmp_project, "doctor", sink="sqlite")

    # Checking an old store conjures nothing: no layout, no coordination tables,
    # no revision column and no marker.
    assert not (arbite / "coordination").exists()
    assert not (arbite / "workspace-binding.json").exists()
    assert "coordination_records" not in _tables(arbite / SQLITE_FILENAME)
    assert "revision" not in _columns(arbite / SQLITE_FILENAME, "tickets")


# ---------------------------------------------------------------------------
# 3. Corrupted-artifact diagnostics
# ---------------------------------------------------------------------------


def test_corrupt_artifact_blob_is_reported_and_never_repaired(sink, arbite_dir):
    state = populate(sink, arbite_dir)
    store = sink.coordination()
    digest = state["artifact"].digest
    assert store.read_artifact_bytes(digest) == b"hello evidence"

    tampered = b"tampered evidence!!"
    tamper_artifact_blob(sink.kind, arbite_dir, digest, tampered)

    problems = [
        problem
        for problem in coordination_problems(sink)
        if problem.kind == "coordination_artifact_corrupt"
    ]
    assert any(digest in problem.detail for problem in problems)

    fixed = coordination_problems(sink, fix=True)
    remaining = [
        problem
        for problem in fixed
        if problem.kind == "coordination_artifact_corrupt"
    ]
    assert remaining and all(problem.fixed is False for problem in remaining)
    # The wired doctor agrees, and neither report nor fix removes the evidence.
    assert any(
        problem.kind == "coordination_artifact_corrupt"
        for problem in sink.check(fix=True)
    )
    assert stored_artifact_bytes(sink.kind, arbite_dir, digest) == tampered


def test_missing_artifact_bytes_are_reported_and_never_restored(sink, arbite_dir):
    state = populate(sink, arbite_dir)
    store = sink.coordination()
    digest = state["artifact"].digest

    remove_artifact_blob(sink.kind, arbite_dir, digest)

    problems = [
        problem
        for problem in coordination_problems(sink)
        if problem.kind == "coordination_missing_artifact"
    ]
    assert any(digest in problem.detail for problem in problems)

    fixed = coordination_problems(sink, fix=True)
    remaining = [
        problem
        for problem in fixed
        if problem.kind == "coordination_missing_artifact"
    ]
    assert remaining and all(problem.fixed is False for problem in remaining)
    assert store.has_artifact(digest) is False
    assert stored_artifact_bytes(sink.kind, arbite_dir, digest) is None


# ---------------------------------------------------------------------------
# 4. Pending-intent diagnostics against the real workspace bytes
# ---------------------------------------------------------------------------


def seed_intents(sink, arbite_dir):
    """Real files under the workspace root plus intents in each observation state."""
    state = populate(sink, arbite_dir)
    store = sink.coordination()
    workspace_id = state["workspace"].id
    root = arbite_dir.parent
    (root / "src").mkdir(parents=True, exist_ok=True)
    before_bytes = b"before\n"
    after_bytes = b"after\n"

    (root / "src" / "before.py").write_bytes(before_bytes)
    (root / "src" / "applied.py").write_bytes(after_bytes)
    (root / "src" / "finalized.py").write_bytes(after_bytes)
    (root / "src" / "drifted.py").write_bytes(b"neither\n")

    receipt = c.OperationReceipt(
        id=c.new_operation_id(),
        attempt_id=state["attempt"].id,
        ticket_id="tic-a1b2",
        actor="tester",
        kind_="write",
        timestamp=c.utc_now(),
    )
    before_intent = c.OperationIntent(
        id=c.new_record_id("operation_intent"),
        operation_id=c.new_operation_id(),
        workspace_id=workspace_id,
        attempt_id=state["attempt"].id,
        ticket_id="tic-a1b2",
        actor="tester",
        kind_="write",
        created=c.utc_now(),
        updated=c.utc_now(),
        before={"src/before.py": c.digest_of_bytes(before_bytes)},
        after={"src/before.py": c.digest_of_bytes(after_bytes)},
        paths=["src/before.py"],
        state="pending",
    )
    applied_intent = c.OperationIntent(
        id=c.new_record_id("operation_intent"),
        operation_id=c.new_operation_id(),
        workspace_id=workspace_id,
        attempt_id=state["attempt"].id,
        ticket_id="tic-a1b2",
        actor="tester",
        kind_="write",
        created=c.utc_now(),
        updated=c.utc_now(),
        before={"src/applied.py": c.digest_of_bytes(before_bytes)},
        after={"src/applied.py": c.digest_of_bytes(after_bytes)},
        paths=["src/applied.py"],
        state="pending",
    )
    finalized_intent = c.OperationIntent(
        id=c.new_record_id("operation_intent"),
        operation_id=receipt.id,
        workspace_id=workspace_id,
        attempt_id=state["attempt"].id,
        ticket_id="tic-a1b2",
        actor="tester",
        kind_="write",
        created=c.utc_now(),
        updated=c.utc_now(),
        before={"src/finalized.py": c.digest_of_bytes(before_bytes)},
        after={"src/finalized.py": c.digest_of_bytes(after_bytes)},
        paths=["src/finalized.py"],
        state="pending",
    )
    drifted_intent = c.OperationIntent(
        id=c.new_record_id("operation_intent"),
        operation_id=c.new_operation_id(),
        workspace_id=workspace_id,
        attempt_id=state["attempt"].id,
        ticket_id="tic-a1b2",
        actor="tester",
        kind_="write",
        created=c.utc_now(),
        updated=c.utc_now(),
        before={"src/drifted.py": c.digest_of_bytes(before_bytes)},
        after={"src/drifted.py": c.digest_of_bytes(after_bytes)},
        paths=["src/drifted.py"],
        state="pending",
    )
    with store.transaction() as tx:
        tx.put(receipt)
        tx.put(before_intent)
        tx.put(applied_intent)
        tx.put(finalized_intent)
        tx.put(drifted_intent)

    files = {
        "src/before.py": (root / "src" / "before.py").read_bytes(),
        "src/applied.py": (root / "src" / "applied.py").read_bytes(),
        "src/finalized.py": (root / "src" / "finalized.py").read_bytes(),
        "src/drifted.py": (root / "src" / "drifted.py").read_bytes(),
    }
    return {
        "before": before_intent,
        "applied": applied_intent,
        "finalized": finalized_intent,
        "drifted": drifted_intent,
        "files": files,
    }


def test_pending_intents_distinguish_the_three_observations(sink, arbite_dir):
    intents = seed_intents(sink, arbite_dir)
    problems = [
        problem
        for problem in coordination_problems(sink)
        if problem.kind == "coordination_pending_intent"
    ]
    assert problems
    assert all(problem.fixed is False for problem in problems)

    def details_for(intent) -> list:
        return [problem.detail for problem in problems if intent.id in problem.detail]

    before_details = details_for(intents["before"])
    applied_details = details_for(intents["applied"])
    finalized_details = details_for(intents["finalized"])
    drifted_details = details_for(intents["drifted"])

    # (a) observed == before -> the detail says "before" and can revert.
    assert before_details and any("before" in d and "reverted" in d for d in before_details)
    # (b) observed == after -> the detail says "after"; terminal state depends on
    #     a matching receipt.
    assert applied_details and any("after" in d and "applied" in d for d in applied_details)
    assert finalized_details and any(
        "after" in d and "finalized" in d for d in finalized_details
    )
    # (c) match neither -> drifted, and it is never guessed at.
    assert drifted_details and any("drifted" in d for d in drifted_details)
    assert not any("drifted" in d for d in before_details + applied_details + finalized_details)


def test_fix_sets_the_unambiguous_intent_states_and_leaves_drift_untouched(sink, arbite_dir):
    intents = seed_intents(sink, arbite_dir)
    store = sink.coordination()

    fixed = coordination_problems(sink, fix=True)
    pending = [
        problem
        for problem in fixed
        if problem.kind == "coordination_pending_intent"
    ]
    by_detail = {problem.detail: problem for problem in pending}
    assert any(intents["before"].id in d and p.fixed for d, p in by_detail.items())
    assert any(intents["applied"].id in d and p.fixed for d, p in by_detail.items())
    assert any(intents["finalized"].id in d and p.fixed for d, p in by_detail.items())
    assert any(intents["drifted"].id in d and not p.fixed for d, p in by_detail.items())

    with store.transaction(write=False) as tx:
        assert tx.get("operation_intent", intents["before"].id).state == "reverted"
        assert tx.get("operation_intent", intents["applied"].id).state == "applied"
        assert tx.get("operation_intent", intents["finalized"].id).state == "finalized"
        assert tx.get("operation_intent", intents["drifted"].id).state == "pending"

    # No workspace file was touched: evidence is preserved exactly.
    root = arbite_dir.parent
    for relative, expected in intents["files"].items():
        assert (root / relative).read_bytes() == expected


# ---------------------------------------------------------------------------
# 5. Migration failure mid-way
# ---------------------------------------------------------------------------


def _in_process_migrate(project_root, source_kind, target_kind, monkeypatch):
    monkeypatch.chdir(project_root)
    monkeypatch.setenv("ARBITE_SINK", source_kind)
    parser, _ = cli.build_parser()
    args = parser.parse_args(["migrate", "--to", target_kind, "--coordination"])
    args.func(args)


def test_cli_migration_failure_midway_leaves_source_intact_and_destination_unbound(
    tmp_project, monkeypatch
):
    state = seed_coordination(tmp_project, "file")
    workspace_id = state["workspace"].id
    source_sink = FileSink(tmp_project / ".arbite")
    source_store = state["store"]

    before_export = x.export_coordination(source_sink, workspace_id=workspace_id)
    before_records = json.dumps(before_export["records"], sort_keys=True)
    before_events = json.dumps(
        [(e["id"], e["event_kind"]) for e in before_export["events"]]
    )
    before_artifacts = {
        entry["digest"]: source_store.read_artifact_bytes(entry["digest"])
        for entry in before_export["artifacts"]
    }
    marker_before = (tmp_project / ".arbite" / "workspace-binding.json").read_bytes()
    config_before = config_bytes(tmp_project)

    def boom(*args, **kwargs):
        raise RuntimeError("injected import failure")

    monkeypatch.setattr(x, "import_coordination", boom)

    with pytest.raises(RuntimeError):
        _in_process_migrate(tmp_project, "file", "sqlite", monkeypatch)

    # The source is byte-identical: records, events and artifact bytes.
    after_export = x.export_coordination(source_sink, workspace_id=workspace_id)
    assert json.dumps(after_export["records"], sort_keys=True) == before_records
    assert (
        json.dumps([(e["id"], e["event_kind"]) for e in after_export["events"]])
        == before_events
    )
    assert {
        entry["digest"]: source_store.read_artifact_bytes(entry["digest"])
        for entry in after_export["artifacts"]
    } == before_artifacts

    # The source marker and the config still name the source.
    assert (tmp_project / ".arbite" / "workspace-binding.json").read_bytes() == marker_before
    assert read_marker(tmp_project)["sink_kind"] == "file"
    assert config_bytes(tmp_project) == config_before

    # The destination was not rebound -- the export/import step never completed.
    destination = SqliteSink(tmp_project / ".arbite" / SQLITE_FILENAME)
    assert destination.coordination().store_binding(workspace_id) is None


def test_migrate_refuses_a_bundle_that_fails_verification(tmp_path, monkeypatch):
    src_dir = tmp_path / "src" / ".arbite"
    src_dir.mkdir(parents=True)
    source = make_sink("file", src_dir)
    state = populate(source, src_dir)
    workspace_id = state["workspace"].id

    tampered = copy.deepcopy(x.export_coordination(source, workspace_id=workspace_id))
    # Knock out a required top-level key: structural verification must refuse it.
    del tampered["records"]
    monkeypatch.setattr(x, "export_coordination", lambda *args, **kwargs: tampered)

    dst_dir = tmp_path / "dst" / ".arbite"
    dst_dir.mkdir(parents=True)
    target = make_sink("file", dst_dir, initialise=False)

    with pytest.raises(CoordinationConflict) as excinfo:
        x.migrate_coordination(source, target, workspace_id=workspace_id)

    problems = excinfo.value.details.get("problems", [])
    assert problems
    assert any(problem["kind"] == "coordination_bundle_invalid" for problem in problems)
    # Verified before anything was written: the destination was never initialised.
    assert target.coordination().is_initialised() is False


# ---------------------------------------------------------------------------
# 6. Active-state refusal
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("blocker", ("attempt", "claim", "intent"))
def test_migrate_coordination_refuses_each_active_state(kind, blocker, arbite_dir):
    sink = make_sink(kind, arbite_dir)
    state = seed_coordination_blocker(sink, arbite_dir, blocker)
    workspace_id = state["workspace"].id

    target = make_sink(other_kind(kind), arbite_dir, initialise=False)
    with pytest.raises(CoordinationConflict) as excinfo:
        x.migrate_coordination(sink, target, workspace_id=workspace_id)

    assert state["blocker_id"] in str(excinfo.value)
    assert not target.coordination().is_initialised()


def test_intent_blocker_state_is_really_active(kind, arbite_dir):
    sink = make_sink(kind, arbite_dir)
    state = seed_coordination_blocker(sink, arbite_dir, "intent")
    intent = state["intent"]
    assert intent.state in c.INTENT_ACTIVE_STATES


def seed_coordination_blocker(sink, arbite_dir, blocker):
    """Add one active thing to a populated store and return what to name."""
    state = populate(sink, arbite_dir)
    store = sink.coordination()
    workspace_id = state["workspace"].id
    now = c.utc_now()
    blocker_id = None
    if blocker == "attempt":
        live = c.WorkAttempt(
            id=c.new_record_id("work_attempt"),
            ticket_id="tic-a1b2",
            worker_id="tester",
            workspace_id=workspace_id,
            generation=2,
            started=now,
            last_activity=now,
        )
        with store.transaction() as tx:
            tx.put(live)
        blocker_id = live.id
    elif blocker == "claim":
        live = c.FileClaim(
            id=c.new_record_id("file_claim"),
            workspace_id=workspace_id,
            path="src/busy.py",
            ticket_id="tic-a1b2",
            attempt_id=state["attempt"].id,
            generation=2,
            acquired=now,
            observed_version=c.digest_of_bytes(b"old"),
        )
        with store.transaction() as tx:
            tx.put(live)
        blocker_id = "src/busy.py"
    elif blocker == "intent":
        live = c.OperationIntent(
            id=c.new_record_id("operation_intent"),
            operation_id=c.new_operation_id(),
            workspace_id=workspace_id,
            attempt_id=state["attempt"].id,
            ticket_id="tic-a1b2",
            actor="tester",
            kind_="write",
            created=now,
            updated=now,
            before={"src/a.py": c.digest_of_bytes(b"old")},
            after={"src/a.py": c.digest_of_bytes(b"new")},
            paths=["src/a.py"],
            state="pending",
        )
        with store.transaction() as tx:
            tx.put(live)
        blocker_id = live.id
    state = dict(state)
    state["blocker_id"] = blocker_id
    state["intent"] = live if blocker == "intent" else None
    return state


@pytest.mark.parametrize("blocker", ("attempt", "claim", "intent"))
def test_cli_migrate_refuses_each_active_state(tmp_project, kind, blocker):
    state = seed_coordination(tmp_project, kind, blocker=blocker)
    target = other_kind(kind)

    proc = run_cli(
        tmp_project, "migrate", "--to", target, "--coordination", sink=kind, expect=1
    )
    assert "not quiescent" in proc.stderr
    assert state["blocker_id"] in proc.stderr

    # Nothing was copied into the destination -- it was never initialised.
    if target == "sqlite":
        assert not (tmp_project / ".arbite" / SQLITE_FILENAME).exists()
    else:
        assert not (tmp_project / ".arbite" / "open").exists()


# ---------------------------------------------------------------------------
# 7. Idempotent re-import / overwrite / re-run
# ---------------------------------------------------------------------------


def test_repeat_import_does_not_duplicate_events_or_namespaces(sink, arbite_dir):
    state = populate(sink, arbite_dir)
    bundle = x.export_coordination(sink, workspace_id=state["workspace"].id)

    target = make_sink(other_kind(sink.kind), arbite_dir)
    target_store = target.coordination()

    first = x.import_coordination(target, bundle)
    event_count = len(target_store.event_log())
    namespace_count = len(target_store.namespaces())
    assert first["events"] == len(bundle["events"])

    second = x.import_coordination(target, bundle)

    assert len(target_store.event_log()) == event_count
    assert second["events"] == 0
    assert second["skipped_existing"] > 0
    assert len(target_store.namespaces()) == namespace_count == 1


def test_overwrite_rewrites_a_differing_record_but_plain_import_preserves_it(
    sink, arbite_dir
):
    state = populate(sink, arbite_dir)
    source_attempt = state["attempt"]
    bundle = x.export_coordination(sink, workspace_id=state["workspace"].id)

    target = make_sink(other_kind(sink.kind), arbite_dir)
    target_store = target.coordination()
    x.import_coordination(target, bundle)

    # A local edit makes the destination record differ from the bundle's copy.
    with target_store.transaction() as tx:
        stored = tx.get("work_attempt", source_attempt.id)
        stored.worker_id = "local-editor"
        tx.put(stored, expect_revision=None)
    edited_revision = _revision_of(target_store, "work_attempt", source_attempt.id)

    without = x.import_coordination(target, bundle, overwrite=False)
    assert without["skipped_existing"] >= 1
    with target_store.transaction(write=False) as tx:
        assert tx.get("work_attempt", source_attempt.id).worker_id == "local-editor"

    with_overwrite = x.import_coordination(target, bundle, overwrite=True)
    assert with_overwrite["records"]["work_attempts"] >= 1
    with target_store.transaction(write=False) as tx:
        assert tx.get("work_attempt", source_attempt.id).worker_id == "tester"
    assert _revision_of(target_store, "work_attempt", source_attempt.id) > edited_revision


def test_cli_migrate_coordination_re_run_is_idempotent(tmp_project):
    state = seed_coordination(tmp_project, "file")
    workspace_id = state["workspace"].id

    first = run_cli(tmp_project, "migrate", "--to", "sqlite", "--coordination", sink="file")
    assert "transferred coordination history" in first.stdout
    assert "destination verified" in first.stdout

    destination_sink = SqliteSink(tmp_project / ".arbite" / SQLITE_FILENAME)
    destination_store = destination_sink.coordination()
    attempt_count = len(_find(destination_store, "work_attempt"))
    claim_count = len(_find(destination_store, "file_claim"))
    event_count = len(destination_store.event_log())
    assert attempt_count == 1

    second = run_cli(tmp_project, "migrate", "--to", "sqlite", "--coordination", sink="file")
    assert "transferred coordination history" in second.stdout
    assert "destination verified" in second.stdout

    # Re-running is safe: no duplicate attempts/claims/events anywhere.
    assert len(_find(destination_store, "work_attempt")) == attempt_count
    assert len(_find(destination_store, "file_claim")) == claim_count
    assert len(destination_store.event_log()) == event_count
    assert len(destination_store.namespaces()) == 1
    assert destination_store.store_binding(workspace_id) is not None
