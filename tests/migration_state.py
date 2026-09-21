"""The states the coordination-migration and integrity tests own.

No frozen transcript covers a store *transfer*, so these builders exist to make the state the
tests compare rather than to match a block. A command that can produce the state produces it
(`claim` for a live attempt, `file claim`/`file read`/`file write` for claims, observations,
receipts and artifacts, `doctor --fix` for releasing a claim whose attempt has ended); the
store API is used only for state no command produces any more -- an attempt a fixture declares
ended, a revision-1 document, a revision counter whose record is gone.

The two backends are both exercised: `SINKS` is what the round trip and the parity assertions
parametrize over, and the bookkeeping each one keeps -- the file backend's revision counters
and commit journal, the SQLite backend's revision rows -- is damaged in its own terms, because
that is the difference the refinement is about.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import claims_state as claims
import evidence_state as evidence
import examples
import lifecycle_state as lifecycle
from arbite.coordination import records as coordination_records
from arbite.coordination.store import open_coordination_store

SINKS = ("file", "sqlite")

HOLDER_TICKET = claims.HOLDER_TICKET
HOLDER_WORKER = claims.HOLDER_WORKER
RIVAL_TICKET = claims.RIVAL_TICKET
RIVAL_WORKER = claims.RIVAL_WORKER
BASE_PY = evidence.BASE_PY
SCHEMA_PY = claims.SCHEMA_PY


def sink_for(project: Path, sink_kind: str = "file"):
    """The project's ticket sink, as the CLI resolves it."""
    return lifecycle.sink_for(project, sink_kind)


def store_for(project: Path, sink_kind: str = "file"):
    """The project's coordination store, as the CLI resolves it."""
    return open_coordination_store(sink_for(project, sink_kind))


def run(project: Path, *args, sink_kind: str | None = None, expect=0):
    """Run one arbite command in the project, asserting the exit code.

    `sink_kind` defaults to the sink the project committed to, so a test parametrized over both
    sinks addresses whichever store the fixture actually used; a test that means one specific
    store names it.

    `expect=None` runs the command without asserting a code, which is what a fixture doing
    setup needs when the command's *effect* is the point and its report is not."""
    proc = examples.run_cli(project, *args, sink=sink_kind)
    if expect is not None:
        assert proc.returncode == expect, (
            f"arbite {' '.join(args)} exited {proc.returncode}, expected {expect}\n"
            f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )
    return proc


def sink_of(project: Path) -> str:
    """Which sink the project committed to, read from its own config."""
    text = (project / ".arbite" / "project.yaml").read_text(encoding="utf-8")
    return text.split(":", 1)[1].strip() if ":" in text else "file"


def released_work(project: Path, ticket_id: str, sink_kind: str = "file") -> str:
    """One record of *history* in a store: a released claim, and return its path.

    What "the destination already holds work" means without inventing live ownership: a claim
    the store remembers and nobody can use. Written through the store because that is the state
    -- there is no command that releases a claim for an attempt that does not exist."""
    store = store_for(project, sink_kind)
    workspace = store.get_workspace()
    assert workspace is not None, "arbite init records the workspace"
    path = "src/arbite/sinks/historical.py"
    now = coordination_records.utc_now()
    store.put_record(
        coordination_records.FileClaim(
            id=coordination_records.claim_id_for(workspace.id, path),
            workspace_id=workspace.id,
            path=path,
            ticket_id=ticket_id,
            attempt_id="att-0000",
            generation=1,
            acquired=now,
            observed_version=coordination_records.ABSENT,
            state=coordination_records.CLAIM_RELEASED,
            released=now,
            release_reason="the destination's own history",
        )
    )
    return path


def snapshot(store) -> dict:
    """Every record a store holds, as its stored document and its write counter.

    The comparison a round trip needs is not "the same number of records" but *the same
    records*: ids, generations, actors, operation ids, event cursors and the revisions a read
    token means. Documents are read unvalidated (`raw_documents`) so this works on a store
    written by an older arbite too."""
    return {
        record_type: {
            record_id: (document, store.revision(record_type, record_id))
            for record_id, document in store.raw_documents(record_type)
        }
        for record_type in coordination_records.RECORD_TYPES
    }


def count(store, record_type: str) -> int:
    return len(store.records(record_type))


# ---------------------------------------------------------------------------
# Quiescent: records of every kind, nothing live
# ---------------------------------------------------------------------------


def quiescent(tmp_path: Path, sink_kind: str = "file") -> Path:
    """A project holding one attempt's five operations, with nothing live.

    EV1's world is the richest one the fixtures build -- a write, a creation, an edit and its
    revert, and an operation a recovery pass finalised as failed, with the claims, read
    observations, receipts and artifacts behind them -- so it is what a transfer has to carry
    without losing anything. `doctor --fix` ends the fixture's ownership (it releases the
    claims of the attempts this builder declares ended), which is the real command for that
    state and leaves the released claims in the store as the history a copy must keep."""
    project = evidence.ev_project(tmp_path, sink_kind)
    store = store_for(project, sink_kind)
    attempts = [
        attempt
        for attempt in store.records("attempt")
        if attempt.ticket_id in (HOLDER_TICKET, RIVAL_TICKET)
    ]
    assert attempts, "the fixture builds the attempts the transcripts name"
    for attempt in attempts:
        store.put_record(
            replace(
                attempt,
                state="released",
                ended=coordination_records.utc_now(),
                outcome="released",
                handoff="the round trip starts from a quiescent store",
            )
        )
    run(project, "doctor", "--fix", sink_kind=sink_kind, expect=None)
    assert store.active_attempts() == [] and store.active_claims() == []
    return project


# ---------------------------------------------------------------------------
# Live: a claimed ticket and a claimed path
# ---------------------------------------------------------------------------


def live(tmp_path: Path, sink_kind: str = "file") -> tuple:
    """A project with live work: an active attempt on a claimed ticket, and an active claim.

    Both through the real commands, because "live" is exactly what they record. Returns
    `(project, attempt_id)`."""
    project = lifecycle.initialise(tmp_path, sink_kind)
    lifecycle.put(
        project,
        HOLDER_TICKET,
        sink_kind,
        title=lifecycle.C03_TITLE,
        priority=1,
        **lifecycle.epic_ticket(),
    )
    claimed = json.loads(
        run(
            project,
            "claim",
            HOLDER_TICKET,
            "--agent",
            HOLDER_WORKER,
            "--json",
            sink_kind=sink_kind,
        ).stdout
    )
    attempt = claimed["attempt"]["id"]
    claims.source_file(project, SCHEMA_PY, lines=20)
    run(
        project,
        "file",
        "claim",
        SCHEMA_PY,
        "--ticket",
        HOLDER_TICKET,
        "--attempt",
        attempt,
        sink_kind=sink_kind,
    )
    return project, attempt


# ---------------------------------------------------------------------------
# The findings a report can hold, built one at a time
# ---------------------------------------------------------------------------


#: The ids the integrity fixtures use. Four hex characters, like every other id here, and
#: distinct from the ones the frozen transcripts name, so a report's own words are the assertion.
CLOSED_TICKET = "tic-c12a"
LIVE_ATTEMPT = "att-c12a"
CLAIM_PY = "src/arbite/sinks/historical.py"
DRIFT_PY = "src/arbite/sinks/drifted.py"
DRIFT_OPERATION = "op-c12a"

#: The two versions a drifted operation recorded, and the third one on disk.
RECORDED_BEFORE = b"the version this operation read\n"
RECORDED_AFTER = b"the version this operation meant to write\n"
DRIFTED = b"somebody else wrote this\n"


def attempt_on_closed_ticket(tmp_path: Path, sink_kind: str = "file") -> tuple:
    """A closed ticket with an attempt that is still active; returns `(project, attempt)`.

    The attempt is written through the store because no command leaves this state: a close ends
    its attempt (the lifecycle cascade) and a takeover revokes the old generation, so an active
    attempt on a closed ticket is exactly the inconsistency `doctor` exists to report."""
    project = lifecycle.initialise(tmp_path, sink_kind)
    lifecycle.put(
        project,
        CLOSED_TICKET,
        sink_kind,
        title="a ticket that was closed",
        priority=1,
        **lifecycle.epic_ticket(),
    )
    run(project, "close", CLOSED_TICKET, sink_kind=sink_kind)
    attempt = claims.put_attempt(project, LIVE_ATTEMPT, CLOSED_TICKET, HOLDER_WORKER, sink_kind)
    return project, attempt


def orphaned_claim(tmp_path: Path, sink_kind: str = "file") -> tuple:
    """A claim held by an attempt that has ended: the shared finding both sinks report.

    Claimed through the real command, then the attempt is declared released -- the state a
    lifecycle command that died half way leaves, and the one a repair releases."""
    project = lifecycle.initialise(tmp_path, sink_kind)
    lifecycle.put(
        project,
        HOLDER_TICKET,
        sink_kind,
        title=lifecycle.C04_TITLE,
        priority=1,
        status="in_progress",
        assignee=HOLDER_WORKER,
        **lifecycle.epic_ticket(),
    )
    claims.put_attempt(project, claims.HOLDER, HOLDER_TICKET, HOLDER_WORKER, sink_kind)
    claims.source_file(project, CLAIM_PY, lines=20)
    run(
        project,
        "file",
        "claim",
        CLAIM_PY,
        "--ticket",
        HOLDER_TICKET,
        "--attempt",
        claims.HOLDER,
        sink_kind=sink_kind,
    )
    store = store_for(project, sink_kind)
    store.put_record(
        replace(
            store.get_attempt(claims.HOLDER),
            state="released",
            ended=coordination_records.utc_now(),
            outcome="released",
        )
    )
    return project, CLAIM_PY


def drifted_pending_operation(
    project: Path, ticket_id: str, path: str = DRIFT_PY, sink_kind: str = "file"
) -> str:
    """A receipt that staged an operation and was never finalised, over bytes that are neither
    of its two recorded versions -- the one finding no repair may act on. Returns its id."""
    store = store_for(project, sink_kind)
    target = project / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(DRIFTED)
    store.put_record(
        coordination_records.OperationReceipt(
            id=DRIFT_OPERATION,
            kind="write",
            paths=[path],
            result=coordination_records.RECEIPT_PENDING,
            recorded_at=coordination_records.utc_now(),
            ticket_id=ticket_id,
            actor=HOLDER_WORKER,
            before={path: coordination_records.digest_bytes(RECORDED_BEFORE)},
            after={path: coordination_records.digest_bytes(RECORDED_AFTER)},
            claim_generation=1,
        )
    )
    return DRIFT_OPERATION


def damaged_note_index(project: Path, ticket_id: str) -> None:
    """Give one ticket a note through the CLI, then delete what the sink indexed for it.

    `ticket_notes` is the SQLite *sink's* own index of the `## Notes` section, so this damage
    belongs to that sink rather than to the coordination store -- and the note has to exist in
    the body first, or "zero rows against zero notes" would be no drift at all."""
    run(
        project,
        "note",
        ticket_id,
        HOLDER_WORKER,
        "a note the index is about to lose",
        sink_kind="sqlite",
    )
    conn = sqlite3.connect(str(sink_for(project, "sqlite").root))
    try:
        conn.execute("DELETE FROM ticket_notes WHERE ticket_id = ?", (ticket_id,))
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Older schema revisions, written by hand
# ---------------------------------------------------------------------------


def old_revision_documents(project: Path, sink_kind: str = "file") -> list:
    """Rewrite the fixture's claim and observation records at schema revision 1.

    Revision 1 had no `spent_by` on a read observation, so a revision-1 observation document
    is the current one with that field removed -- and a revision-1 claim is the current
    document with only its `schema_revision` changed, which is what the bump means for every
    other record type. Written by editing the store's own storage, because that *is* the state
    a store written by an older arbite is in and no command can produce it (or read it) any
    more. Returns the `(record_type, record_id)` pairs that were written."""
    store = store_for(project, sink_kind)
    rewritten = []
    for record_type in ("claim", "observation"):
        for record_id, document in store.raw_documents(record_type):
            older = {**document, "schema_revision": 1}
            older.pop("spent_by", None)
            write_document(project, record_type, older, sink_kind)
            rewritten.append((record_type, record_id))
    return rewritten


def write_document(project: Path, record_type: str, document: dict, sink_kind: str = "file") -> None:
    """Store one document exactly as given, including an older schema revision.

    Each backend is edited in its own terms -- a JSON document beside the others for the file
    backend, the `coordination_records` row for SQLite -- because the point is to reach a state
    arbite's own writes refuse to produce."""
    store = store_for(project, sink_kind)
    record_id = document["id"]
    if sink_kind == "file":
        path = store.record_path(coordination_records.read_forward(document))
        path.write_text(_document_text(document), encoding="utf-8")
        return
    conn = sqlite3.connect(str(store.root))
    try:
        conn.execute(
            "UPDATE coordination_records SET document = ? "
            "WHERE record_type = ? AND record_id = ?",
            (_document_text(document), record_type, record_id),
        )
        conn.commit()
    finally:
        conn.close()


def _document_text(document: dict) -> str:
    """The stored form of a document, written the way both backends write it."""
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


# ---------------------------------------------------------------------------
# The backend's own bookkeeping, damaged in its own terms
# ---------------------------------------------------------------------------


def orphan_revision_counter(project: Path, sink_kind: str = "file") -> str:
    """A counter for a record that is not there, in whichever store keeps it that way.

    The file backend keeps its counters in `revisions.json`, the SQLite backend in a table;
    each is edited in its own terms, because "a counter nobody can match to a record" is a
    finding about *that* storage rather than about a record. Returns the key a report names."""
    store = store_for(project, sink_kind)
    if sink_kind == "file":
        revisions = store._read_revisions()
        revisions[ORPHAN_COUNTER] = 3
        store._write_revisions(revisions)
        return ORPHAN_COUNTER
    conn = sqlite3.connect(str(store.root))
    try:
        conn.execute(
            "INSERT INTO coordination_revisions (record_type, record_id, revision) "
            "VALUES (?, ?, ?)",
            tuple(ORPHAN_COUNTER.split("/")) + (3,),
        )
        conn.commit()
    finally:
        conn.close()
    return ORPHAN_COUNTER


#: The counter the fixtures leave behind: a key neither backend has a record for.
ORPHAN_COUNTER = "claim/clm-0000"


def stored_document(project: Path, record_type: str, record_id: str, sink_kind: str = "file"):
    """One stored document, unvalidated, as the store holds it."""
    return dict(store_for(project, sink_kind).raw_documents(record_type))[record_id]


def commit_journal(project: Path, sink_kind: str = "file") -> Path:
    """A commit journal a process left behind, written where the file backend keeps it.

    The file backend stages a unit of work as `commit-journal.json` and removes it last, so a
    journal on disk is the state a process killed mid-commit leaves -- and the state `doctor`
    has to report, because replaying it is the *next write*'s job rather than a repair's. The
    entry is a delete of a record that is not there, so a later write that does replay it
    changes nothing either."""
    store = store_for(project, sink_kind)
    journal = {
        "operation_id": None,
        "written_at": coordination_records.utc_now(),
        "writes": [
            {
                "action": "delete",
                "record_type": "claim",
                "record_id": "clm-0000",
                "revision": None,
                "cursor": None,
                "document": None,
            }
        ],
    }
    path = Path(store.root) / "commit-journal.json"
    path.write_text(json.dumps(journal, indent=2) + "\n", encoding="utf-8")
    return path
