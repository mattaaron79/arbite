"""Projects in the states the file-claim transcripts describe.

The FC blocks are about *ownership*, so the state in front of them is the whole
subject: which tickets are in progress, which attempt each names, what the files on
disk contain, and what has already been acquired. These builders make that state
explicit -- through the real CLI wherever a command is supposed to produce it (every
acquisition and release in the FC chain), and through the sink/store where the state is
one the document simply declares.

Two ids in the transcripts are tokens the commands *name*, so they must exist for the
command to be runnable at all: the holder attempt `att-91bd` and the rival attempt
`att-4c81` (plus the tickets `tic-cf9f`, `tic-9b57` and `tic-1a75`). They are seeded
with the document's own ids, exactly as the ticket ids are, so the fixture holds what
the transcript talks about; the harness normalises both sides anyway.

The file *contents* are chosen for the line counts the transcripts print (412 lines for
`sinks/file.py`, 570 for `sinks/base.py`), because those are asserted literally. Digests
are normalised by the harness, and the tests assert the real relationship instead: a
claim's `observed_version` is the digest of those very bytes.
"""

from __future__ import annotations

from pathlib import Path

import examples
import lifecycle_state as state
from arbite.coordination import records as coordination_records
from arbite.coordination.store import open_coordination_store

EPIC = state.EPIC
C03_TITLE = state.C03_TITLE
C04_TITLE = state.C04_TITLE
C02_TITLE = "Implement transactional coordination storage and durable events"

#: The attempts and workers the FC transcripts name.
HOLDER = "att-91bd"
HOLDER_WORKER = "claude.opus.001"
RIVAL = "att-4c81"
RIVAL_WORKER = "claude.sonnet.002"
#: FC5's ticket is held by an attempt of its own -- one attempt belongs to one ticket,
#: so the doc's "in_progress for att-91bd" cannot hold two tickets at once. The id is
#: normalised on both sides, and the *fact* the block asserts (a different attempt owns
#: the ticket) is what the fixture reproduces.
FOREIGN = "att-1a75"

HOLDER_TICKET = "tic-cf9f"
RIVAL_TICKET = "tic-9b57"
FOREIGN_TICKET = "tic-1a75"

FILE_PY = "src/arbite/sinks/file.py"
BASE_PY = "src/arbite/sinks/base.py"
SCHEMA_PY = "src/arbite/schema.py"
RECORDS_PY = "src/arbite/coordination/records.py"
OLD_PY = "src/arbite/sinks/old.py"

#: The line counts the FC1/FC2 rows print, so the fixture really does have that shape.
FILE_LINES = 412
BASE_LINES = 570


def coordination(project: Path, sink_kind: str = "file"):
    """The project's coordination store, as the CLI resolves it."""
    return open_coordination_store(state.sink_for(project, sink_kind))


def source_file(project: Path, relative: str, lines: int = 20) -> Path:
    """A source file with exactly `lines` newline-terminated lines."""
    path = project / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(f"# {relative} line {index}\n" for index in range(1, lines + 1)),
        encoding="utf-8",
    )
    return path


def source_dir(project: Path, relative: str) -> Path:
    """An empty directory, for a path the fixture needs to *not* exist yet (FC4)."""
    path = project / relative
    path.mkdir(parents=True, exist_ok=True)
    return path


def put_attempt(
    project: Path,
    attempt_id: str,
    ticket_id: str,
    worker_id: str,
    sink_kind: str = "file",
) -> str:
    """Record one active attempt directly, under the id the transcript names.

    Written through the store because that is what the state is: the transcripts treat
    the attempt as already existing (the ticket was claimed before the block starts),
    so no command has to be re-run to produce it. `arbite attempt adopt` is the nearest
    real command, and it invents no history -- the same thing this does."""
    store = coordination(project, sink_kind)
    workspace = store.get_workspace()
    assert workspace is not None, "arbite init records the workspace"
    now = coordination_records.utc_now()
    store.put_record(
        coordination_records.WorkAttempt(
            id=attempt_id,
            ticket_id=ticket_id,
            worker_id=worker_id,
            workspace_id=workspace.id,
            generation=1,
            state="active",
            started=now,
            last_activity=now,
        )
    )
    return attempt_id


def run(project: Path, *args, sink_kind: str = "file", expect: int = 0):
    """Run one arbite command in the project, asserting the exit code."""
    proc = examples.run_cli(project, *args, sink=sink_kind)
    assert proc.returncode == expect, (
        f"arbite {' '.join(args)} exited {proc.returncode}, expected {expect}\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    return proc


def holder_project(tmp_path: Path, sink_kind: str = "file") -> Path:
    """FC1's world: `tic-cf9f` in progress for `att-91bd`, with the source files.

    `tic-9b57` is in progress for the rival attempt too, because FC3's refusal names it
    and the hint offers releasing it; and FC5's ticket is held by a third attempt."""
    project = state.initialise(tmp_path, sink_kind)
    state.put(
        project,
        HOLDER_TICKET,
        sink_kind,
        title=C03_TITLE,
        status="in_progress",
        assignee=HOLDER_WORKER,
        priority=1,
        **state.epic_ticket(),
    )
    state.put(
        project,
        RIVAL_TICKET,
        sink_kind,
        title=C04_TITLE,
        status="in_progress",
        assignee=RIVAL_WORKER,
        priority=1,
        **state.epic_ticket(),
    )
    state.put(
        project,
        FOREIGN_TICKET,
        sink_kind,
        title=C02_TITLE,
        status="in_progress",
        assignee=HOLDER_WORKER,
        priority=1,
        **state.epic_ticket(),
    )
    put_attempt(project, HOLDER, HOLDER_TICKET, HOLDER_WORKER, sink_kind)
    put_attempt(project, RIVAL, RIVAL_TICKET, RIVAL_WORKER, sink_kind)
    put_attempt(project, FOREIGN, FOREIGN_TICKET, HOLDER_WORKER, sink_kind)
    source_file(project, FILE_PY, FILE_LINES)
    source_file(project, BASE_PY, BASE_LINES)
    source_file(project, SCHEMA_PY, 40)
    # FC4 claims a path that does not exist yet, and RD5 reads one: the directories have
    # to be there for the "not yet" to mean a file rather than a missing tree.
    source_dir(project, "src/arbite/coordination")
    return project


def claimed_file_py(project: Path, sink_kind: str = "file") -> Path:
    """FC1's acquisition, run for real: the holder takes `sinks/file.py` at generation 1."""
    run(
        project,
        "file",
        "claim",
        FILE_PY,
        "--ticket",
        HOLDER_TICKET,
        "--attempt",
        HOLDER,
        sink_kind=sink_kind,
    )
    return project


def claimed_pair(project: Path, sink_kind: str = "file") -> Path:
    """FC1 then FC2: `file.py` at generation 1, then `file.py` + `base.py` at 2.

    Every later block in the document starts from this state -- FC3's busy message, FC4's
    third generation, FC6's two claims, FC7's release of generation 2 and FC8's
    re-acquisition all count from exactly these two acquisitions."""
    claimed_file_py(project, sink_kind)
    run(
        project,
        "file",
        "claim",
        FILE_PY,
        BASE_PY,
        "--ticket",
        HOLDER_TICKET,
        "--attempt",
        HOLDER,
        sink_kind=sink_kind,
    )
    return project


def released_and_schema(project: Path, sink_kind: str = "file") -> Path:
    """FC3/FC6's world: the pair, `file.py` handed back, then `schema.py` at generation 3."""
    claimed_pair(project, sink_kind)
    run(
        project,
        "file",
        "release",
        FILE_PY,
        "--ticket",
        HOLDER_TICKET,
        "--attempt",
        HOLDER,
        "--reason",
        "edits complete",
        sink_kind=sink_kind,
    )
    run(
        project,
        "file",
        "claim",
        SCHEMA_PY,
        "--ticket",
        HOLDER_TICKET,
        "--attempt",
        HOLDER,
        sink_kind=sink_kind,
    )
    return project


def released_base(project: Path, sink_kind: str = "file") -> Path:
    """FC7's world: the pair, then `base.py` released at generation 2."""
    claimed_pair(project, sink_kind)
    run(
        project,
        "file",
        "release",
        BASE_PY,
        "--ticket",
        HOLDER_TICKET,
        "--attempt",
        HOLDER,
        "--reason",
        "edits complete",
        sink_kind=sink_kind,
    )
    return project
