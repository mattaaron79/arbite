"""The state DR1 and DR2 describe: a store with three findings, and a payload area.

Built through the *store* rather than by writing documents into the coordination directory,
so the layout stays the backend's business and the same fixture works on both sinks (the
frozen scenarios are asserted on both). The ids are the document's own wherever its block
names one -- `tic-cf9f`, `tic-1a75`, `att-91bd`, `op-4f19` -- so the real store holds exactly
the records the transcript talks about; the harness normalises ids on both sides anyway, which
is why the claim naming an attempt that does not exist can use a different id from the claim
naming a finished one.

The three findings are the three a recovery report can have:

- an **orphaned claim**: `src/arbite/sinks/base.py` is held by an attempt whose ticket closed;
- a **pending operation** whose bytes are neither of its recorded versions: the file holds a
  third version, so this is drift and no recovery may guess which version is right;
- a **claim naming no attempt**: `src/arbite/schema.py` is held by `att-0000`, which this store
  has never heard of.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from arbite.coordination import records as coordination_records
from arbite.coordination.store import open_coordination_store
from helpers import make_ticket

from examples import run_cli
from lifecycle_state import epic_ticket, sink_for

#: How many tickets the transcript's store holds (DR1: "checked 22 tickets").
TICKETS = 22

#: The payload sizes DR1 reports: three files, 12.4 KiB in total.
SCRATCH_SIZES = (4000, 4000, 4700)

DRIFT_PATH = "src/arbite/sinks/base.py"
DANGLING_PATH = "src/arbite/schema.py"
ORPHANED_TICKET = "tic-cf9f"
OPERATION_TICKET = "tic-1a75"
ATTEMPT = "att-91bd"
MISSING_ATTEMPT = "att-0000"
OPERATION = "op-4f19"

#: What each version is, in the fixture and in the transcript: the operation recorded a
#: before and an after, and somebody else's bytes are what is on disk now.
RECORDED_BEFORE = b"the version this operation read\n"
RECORDED_AFTER = b"the version this operation meant to write\n"
DRIFTED = b"somebody else wrote this\n"


def initialise(tmp_path: Path, sink_kind: str = "file") -> Path:
    """An initialised project whose committed choice is `sink_kind`."""
    project = tmp_path / "project"
    project.mkdir()
    arbite_dir = project / ".arbite"
    arbite_dir.mkdir()
    (arbite_dir / "project.yaml").write_text(f"sink: {sink_kind}\n", encoding="utf-8")
    proc = run_cli(project, "init")
    assert proc.returncode == 0, proc.stderr
    return project


def damaged(tmp_path: Path, sink_kind: str = "file") -> Path:
    """A project holding DR1's findings, a drifted file and three leftover payloads."""
    project = initialise(tmp_path, sink_kind)
    sink = sink_for(project, sink_kind)

    # 22 clean tickets, two of them the ones the coordination records name: an attempt whose
    # ticket is not in the store is its own finding, and the transcript's store is clean.
    today = datetime.now().strftime("%Y-%m-%d")
    sink.create(
        make_ticket("tic-cf9f", status="closed", closed=today, **epic_ticket(title="a closed ticket"))
    )
    sink.create(make_ticket(OPERATION_TICKET, **epic_ticket(title="a ticket with an operation")))
    for index in range(TICKETS - 2):
        sink.create(make_ticket(f"tic-{index + 0x1000:04x}"))

    store = open_coordination_store(sink)
    workspace = store.get_workspace()
    assert workspace is not None, "arbite init records the workspace"

    # The attempt that holds the orphaned claim: it ended because its ticket closed, which is
    # what the attempt's own record says.
    store.put_record(
        coordination_records.WorkAttempt(
            id=ATTEMPT,
            ticket_id=ORPHANED_TICKET,
            worker_id="claude.opus.001",
            workspace_id=workspace.id,
            generation=1,
            state="finished",
            started=coordination_records.utc_now(),
            last_activity=coordination_records.utc_now(),
            ended=coordination_records.utc_now(),
            outcome="closed",
        )
    )

    # The bytes: the drifted third version, in a directory that exists (arbite will not judge a
    # path whose parent is missing as "the version you recorded" -- it is simply not there).
    drifted = project / DRIFT_PATH
    drifted.parent.mkdir(parents=True, exist_ok=True)
    drifted.write_bytes(DRIFTED)
    dangling = project / DANGLING_PATH
    dangling.parent.mkdir(parents=True, exist_ok=True)
    dangling.write_bytes(b"a file nobody claimed under a live attempt\n")

    store.put_record(
        coordination_records.FileClaim(
            id=coordination_records.claim_id_for(workspace.id, DRIFT_PATH),
            workspace_id=workspace.id,
            path=DRIFT_PATH,
            ticket_id=ORPHANED_TICKET,
            attempt_id=ATTEMPT,
            generation=1,
            acquired=coordination_records.utc_now(),
            observed_version=coordination_records.digest_bytes(DRIFTED),
        )
    )
    store.put_record(
        coordination_records.FileClaim(
            id=coordination_records.claim_id_for(workspace.id, DANGLING_PATH),
            workspace_id=workspace.id,
            path=DANGLING_PATH,
            ticket_id=OPERATION_TICKET,
            attempt_id=MISSING_ATTEMPT,
            generation=1,
            acquired=coordination_records.utc_now(),
            observed_version=coordination_records.digest_bytes(dangling.read_bytes()),
        )
    )

    # The unfinished operation: intent and both versions recorded, never finalised, and the
    # bytes on disk are a third version -- the case arbite reports rather than resolves.
    store.put_record(
        coordination_records.OperationReceipt(
            id=OPERATION,
            kind="write",
            paths=[DRIFT_PATH],
            result=coordination_records.RECEIPT_PENDING,
            recorded_at=coordination_records.utc_now(),
            ticket_id=OPERATION_TICKET,
            attempt_id=ATTEMPT,
            actor="claude.opus.001",
            before={DRIFT_PATH: coordination_records.digest_bytes(RECORDED_BEFORE)},
            after={DRIFT_PATH: coordination_records.digest_bytes(RECORDED_AFTER)},
            claim_generation=1,
        )
    )

    scratch = project / ".arbite" / "scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    for index, size in enumerate(SCRATCH_SIZES):
        (scratch / f"payload-{index}.py").write_bytes(b"x" * size)
    return project
